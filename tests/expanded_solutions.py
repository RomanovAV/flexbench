"""Control implementations for the expanded dev suite; never delivered to executors."""

RESET = '''public final class TaskResetService {
 public enum Status { NEW, RUNNING, FAILED, CANCELLED, DONE }
 public record Task(String id,String owner,Status status,int attempt,String error,long version,String lastResetId,String payload) {}
 public record ResetCommand(String actor,boolean admin,long expectedVersion,String requestId) {}
 private static boolean invalid(String value) { return value == null || value.trim().isEmpty(); }
 public static Task reset(Task task,ResetCommand command) {
  if(task==null || command==null || invalid(task.id()) || invalid(task.owner()) || task.status()==null || task.version()<0
    || invalid(command.actor()) || invalid(command.requestId())) throw new IllegalArgumentException();
  if(!command.admin() && !task.owner().equals(command.actor())) throw new SecurityException();
  if(command.requestId().equals(task.lastResetId())) return task;
  if(command.expectedVersion()!=task.version()) throw new IllegalStateException();
  if(task.status()!=Status.FAILED && task.status()!=Status.CANCELLED) throw new IllegalStateException();
  if(task.version()==Long.MAX_VALUE) throw new IllegalStateException();
  return new Task(task.id(),task.owner(),Status.NEW,0,null,task.version()+1,command.requestId(),task.payload());
 }
}
'''

RECOMMENDATIONS = '''import java.time.Instant; import java.util.*;
public final class RecommendationSelector {
 public record Candidate(String id,String taskType,String source,Instant dueAt,int priority,long revision) {}
 public record Rule(String taskType,String source,int quota,boolean carryOver) {}
 private record Key(String type,String source) {}
 private static boolean invalid(String value) { return value==null || value.trim().isEmpty(); }
 public static List<Candidate> select(List<Candidate> candidates,List<Rule> rules,Instant now,int limit) {
  if(candidates==null || rules==null || now==null || limit<0) throw new IllegalArgumentException();
  Map<Key,Rule> index=new HashMap<>();
  for(Rule r:rules) {
   if(r==null || invalid(r.taskType()) || invalid(r.source()) || r.quota()<0) throw new IllegalArgumentException();
   if(index.putIfAbsent(new Key(r.taskType(),r.source()),r)!=null) throw new IllegalArgumentException();
  }
  Map<String,Candidate> winners=new LinkedHashMap<>();
  for(Candidate c:candidates) {
   if(c==null || invalid(c.id()) || invalid(c.taskType()) || invalid(c.source()) || c.dueAt()==null) continue;
   Rule rule=index.get(new Key(c.taskType(),c.source()));
   if(rule==null || rule.quota()==0 || (c.dueAt().isBefore(now) && !rule.carryOver())) continue;
   Candidate old=winners.get(c.id());
   if(old==null || c.revision()>old.revision()) winners.put(c.id(),c);
  }
  List<Candidate> sorted=new ArrayList<>(winners.values());
  sorted.sort(Comparator.comparingInt(Candidate::priority).reversed().thenComparing(Candidate::dueAt).thenComparing(Candidate::id));
  Map<Key,Integer> used=new HashMap<>();List<Candidate> result=new ArrayList<>();
  for(Candidate c:sorted) {
   if(result.size()>=limit) break;
   Key key=new Key(c.taskType(),c.source());
   if(used.getOrDefault(key,0)>=index.get(key).quota()) continue;
   result.add(c);used.merge(key,1,Integer::sum);
  }
  return result;
 }
}
'''

POOL = '''export async function mapLimit(items, concurrency, worker) {
 if (!Array.isArray(items) || typeof worker !== 'function') throw new TypeError();
 if (!Number.isSafeInteger(concurrency) || concurrency < 1) throw new RangeError();
 const results = new Array(items.length); let next = 0;
 async function consume() {
  while (next < items.length) {
   const index = next++;
   try {results[index] = {status:'fulfilled', value:await worker(items[index], index)};}
   catch (reason) {results[index] = {status:'rejected', reason};}
  }
 }
 await Promise.all(Array.from({length:Math.min(concurrency, items.length)}, consume));
 return results;
}
'''

BATCH = '''import {mapLimit} from './map-limit.mjs';
export async function updateSelected(ids, concurrency, update) {
 if (!Array.isArray(ids) || typeof update !== 'function' || ids.some(id=>typeof id !== 'string' || !id.trim())) throw new TypeError();
 if (!Number.isSafeInteger(concurrency) || concurrency < 1) throw new RangeError();
 const unique = [...new Set(ids)];
 const results = await mapLimit(unique, concurrency, id=>update(id));
 return results.map((result, index)=>({id:unique[index], ...result}));
}
'''

INDEXES = '''CREATE INDEX IF NOT EXISTS idx_owner_status_schedule ON activities(owner_id, status, scheduled_at, id);
'''

ENTRYPOINT = '''#!/bin/sh
set -eu
if [ -n "${JAVA_HEAP_MB:-}" ]; then
 case "$JAVA_HEAP_MB" in
  *[!0-9]*|0*) exit 2 ;;
 esac
 set -- "-Xmx${JAVA_HEAP_MB}m" -jar "${APP_JAR:-/opt/service/app.jar}" "$@"
else
 set -- -jar "${APP_JAR:-/opt/service/app.jar}" "$@"
fi
exec "${JAVA_BIN:-java}" "$@"
'''

IDS = ('java-task-reset', 'java-recommendation-rules', 'js-batch-update', 'sql-activity-indexes', 'service-launch-contract')


def apply(case_id, workspace):
    if case_id == 'java-task-reset':
        (workspace / 'src/TaskResetService.java').write_text(RESET)
    elif case_id == 'java-recommendation-rules':
        (workspace / 'src/RecommendationSelector.java').write_text(RECOMMENDATIONS)
    elif case_id == 'js-batch-update':
        (workspace / 'src/map-limit.mjs').write_text(POOL)
        (workspace / 'src/batch-update.mjs').write_text(BATCH)
    elif case_id == 'sql-activity-indexes':
        (workspace / 'migration.sql').write_text(INDEXES)
    elif case_id == 'service-launch-contract':
        for name in ('Dockerfile', 'pom.xml', 'deploy.env'):
            path = workspace / name
            path.write_text(path.read_text().replace('17.0.12', '17.0.13'))
        (workspace / 'entrypoint.sh').write_text(ENTRYPOINT)
    else:
        raise ValueError(case_id)
