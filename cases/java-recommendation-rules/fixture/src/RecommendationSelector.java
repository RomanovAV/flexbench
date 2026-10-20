import java.time.Instant;
import java.util.*;

public final class RecommendationSelector {
    public record Candidate(String id, String taskType, String source, Instant dueAt, int priority, long revision) {}
    public record Rule(String taskType, String source, int quota, boolean carryOver) {}
    public static List<Candidate> select(List<Candidate> candidates, List<Rule> rules, Instant now, int limit) {
        return candidates.stream().filter(Objects::nonNull)
            .sorted(Comparator.comparingInt(Candidate::priority).reversed())
            .limit(limit).toList();
    }
}
