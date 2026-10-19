"""Control solutions for grader sensitivity tests. Never copy into agent workspaces."""

JAVA = '''import java.time.Instant;
import java.util.*;
public final class ActivityService {
 public record Activity(String id, String ownerId, String status, Instant scheduledAt) {}
 public static Optional<Activity> nearestPlanned(List<Activity> activities, String ownerId, Instant now) {
  if (activities == null) return Optional.empty();
  return activities.stream().filter(Objects::nonNull)
    .filter(a -> ownerId.equals(a.ownerId()) && "PLANNED".equals(a.status()))
    .filter(a -> a.scheduledAt() != null && !a.scheduledAt().isBefore(now))
    .min(Comparator.comparing(Activity::scheduledAt).thenComparing(Activity::id));
 }
}
'''

JS = '''export function canRetry(s) { return s.status === 'failed'; }
export function reduceRetry(s, e) {
 if (!e) return s;
 if (e.type === 'START') {
  if (!['idle','failed'].includes(s.status) || typeof e.requestId !== 'string' || !e.requestId) return s;
  return {...s, status:'pending', requestId:e.requestId, error:null, attempts:s.attempts+1};
 }
 if (s.status !== 'pending' || e.requestId !== s.requestId) return s;
 if (e.type === 'SUCCESS') return {...s, status:'succeeded', error:null};
 if (e.type === 'FAILURE') return {...s, status:'failed', error:typeof e.error === 'string' && e.error ? e.error : 'Unknown error'};
 return s;
}
'''


def apply(case_id, workspace):
    if case_id == "java-nearest-activity":
        (workspace / "src/ActivityService.java").write_text(JAVA)
    elif case_id == "js-retry-state":
        (workspace / "src/retry-state.mjs").write_text(JS)
    elif case_id == "maven-version-alignment":
        root = workspace / "pom.xml"
        root.write_text(root.read_text().replace("3.2.0", "3.2.1"))
        child = workspace / "api/pom.xml"
        child.write_text(child.read_text().replace("3.2.0", "${framework.version}"))
    else:
        raise ValueError(case_id)
