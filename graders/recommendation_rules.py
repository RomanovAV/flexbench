import json
from pathlib import Path
import subprocess
import sys
import tempfile

HARNESS = r'''
import java.time.*; import java.util.*;
public class CheckRecommendations {
 static final Instant NOW = Instant.parse("2026-01-01T00:00:00Z");
 static RecommendationSelector.Candidate c(String id,String type,String source,long offset,int priority,long rev) {
  return new RecommendationSelector.Candidate(id,type,source,NOW.plusSeconds(offset),priority,rev);
 }
 static RecommendationSelector.Rule r(String type,String source,int quota,boolean carry) {
  return new RecommendationSelector.Rule(type,source,quota,carry);
 }
 static List<String> ids(List<RecommendationSelector.Candidate> cs,List<RecommendationSelector.Rule> rs,int limit) {
  return RecommendationSelector.select(cs,rs,NOW,limit).stream().map(RecommendationSelector.Candidate::id).toList();
 }
 static boolean bad(Runnable f) { try {f.run();return false;} catch(IllegalArgumentException e){return true;} }
 static void check(String id,java.util.function.BooleanSupplier f) {
  boolean ok;try{ok=f.getAsBoolean();}catch(Throwable e){ok=false;}System.out.println(id+"="+ok);
 }
 public static void main(String[] args) {
  check("composite_rule", () -> ids(List.of(c("wrong","T","X",1,99,1),c("right","T","S",1,1,1)),List.of(r("T","S",5,false)),10).equals(List.of("right")));
  check("carryover_and_boundary", () -> ids(List.of(c("past","T","S",-1,5,1),c("now","T","S",0,4,1),c("carry","T","C",-2,3,1)),List.of(r("T","S",5,false),r("T","C",5,true)),10).equals(List.of("now","carry")));
  check("zero_quota", () -> ids(List.of(c("a","T","S",1,1,1)),List.of(r("T","S",0,true)),10).isEmpty());
  check("quota_per_pair", () -> ids(List.of(c("a","T","S",1,9,1),c("b","T","S",1,8,1),c("c","T","C",1,7,1),c("d","U","S",1,6,1)),List.of(r("T","S",1,true),r("T","C",1,true),r("U","S",1,true)),10).equals(List.of("a","c","d")));
  check("global_limit_after_quota", () -> ids(List.of(c("a","T","S",1,9,1),c("b","T","S",1,8,1),c("c","T","C",1,7,1)),List.of(r("T","S",1,true),r("T","C",2,true)),2).equals(List.of("a","c")));
  check("sort_ties", () -> ids(List.of(c("z","T","S",2,3,1),c("b","T","S",1,3,1),c("a","T","S",1,3,1),c("top","T","S",9,4,1)),List.of(r("T","S",9,true)),10).equals(List.of("top","a","b","z")));
  check("revision_before_ranking", () -> {
   var old=c("x","T","S",1,100,1);var fresh=c("x","T","S",2,1,10);
   var n=RecommendationSelector.select(List.of(old,c("y","T","S",1,2,1),fresh),List.of(r("T","S",9,true)),NOW,10);
   return n.equals(List.of(c("y","T","S",1,2,1),fresh));
  });
  check("first_equal_revision", () -> {
   var first=c("x","T","S",1,1,10);var later=c("x","T","S",2,99,10);
   return RecommendationSelector.select(List.of(first,later),List.of(r("T","S",9,true)),NOW,10).equals(List.of(first));
  });
  check("filter_before_dedupe", () -> {
   var valid=c("x","T","S",1,1,1);var invalid=c("x","T","S",-1,99,100);
   return RecommendationSelector.select(List.of(invalid,valid),List.of(r("T","S",9,false)),NOW,10).equals(List.of(valid));
  });
  check("skip_invalid_candidates", () -> {
   var cs=Arrays.asList(null,c(null,"T","S",1,1,1),c(" ","T","S",1,1,1),c("x",null,"S",1,1,1),c("x"," ","S",1,1,1),c("x","T",null,1,1,1),c("x","T"," ",1,1,1),new RecommendationSelector.Candidate("x","T","S",null,1,1),c("ok","T","S",1,1,1));
   return ids(cs,List.of(r("T","S",5,true)),10).equals(List.of("ok"));
  });
  check("invalid_arguments", () -> bad(() -> RecommendationSelector.select(null,List.of(),NOW,1)) && bad(() -> RecommendationSelector.select(List.of(),null,NOW,1))
    && bad(() -> RecommendationSelector.select(List.of(),List.of(),null,1)) && bad(() -> RecommendationSelector.select(List.of(),List.of(),NOW,-1)));
  check("validate_all_rules", () -> {
   var badRules=Arrays.asList(null,r(null,"S",1,true),r(" ","S",1,true),r("T",null,1,true),r("T","",1,true),r("T","S",-1,true));
   return badRules.stream().allMatch(rule -> bad(() -> RecommendationSelector.select(List.of(),Arrays.asList(rule),NOW,0)));
  });
  check("duplicate_rules", () -> bad(() -> RecommendationSelector.select(List.of(),List.of(r("T","S",1,true),r("T","S",2,false)),NOW,0)));
  check("composite_key_collision", () -> ids(List.of(c("a","a:b","c",1,2,1),c("b","a","b:c",1,1,1)),List.of(r("a:b","c",1,true),r("a","b:c",1,true)),10).equals(List.of("a","b")));
  check("numeric_extremes", () -> {
   var newer=c("x","T","S",1,Integer.MIN_VALUE,Long.MAX_VALUE);
   return RecommendationSelector.select(List.of(c("x","T","S",1,0,Long.MIN_VALUE),newer,c("y","T","S",1,Integer.MAX_VALUE,0)),List.of(r("T","S",9,true)),NOW,10).equals(List.of(c("y","T","S",1,Integer.MAX_VALUE,0),newer));
  });
  check("no_mutation_or_shared_quota", () -> {
   var cs=new ArrayList<>(List.of(c("b","T","S",1,1,1),c("a","T","S",1,2,1)));var before=new ArrayList<>(cs);var rules=List.of(r("T","S",1,true));
   var expected=List.of("a");return ids(cs,rules,1).equals(expected) && ids(cs,rules,1).equals(expected) && cs.equals(before) && ids(cs,rules,0).isEmpty();
  });
 }
}
'''
IDS = ["composite_rule", "carryover_and_boundary", "zero_quota", "quota_per_pair", "global_limit_after_quota",
       "sort_ties", "revision_before_ranking", "first_equal_revision", "filter_before_dedupe", "skip_invalid_candidates",
       "invalid_arguments", "validate_all_rules", "duplicate_rules", "composite_key_collision", "numeric_extremes",
       "no_mutation_or_shared_quota"]


def main():
    workspace = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as directory:
        harness = Path(directory) / "CheckRecommendations.java"
        harness.write_text(HARNESS, encoding="utf-8")
        compiled = subprocess.run(["javac", "--release", "17", "-encoding", "UTF-8", "-d", directory,
                                   *map(str, (workspace / "src").glob("*.java")), str(harness)], capture_output=True, text=True)
        checks = [{"id": "compiles", "passed": compiled.returncode == 0}]
        if compiled.returncode:
            print(json.dumps({"checks": checks, "detail": compiled.stderr[-4000:]}))
            return
        result = subprocess.run(["java", "-cp", directory, "CheckRecommendations"], capture_output=True, text=True)
        lines = set(result.stdout.splitlines())
        checks += [{"id": key, "passed": result.returncode == 0 and key + "=true" in lines} for key in IDS]
        print(json.dumps({"checks": checks, "detail": result.stderr[-4000:]}))


if __name__ == "__main__":
    main()
