import java.time.Instant;
import java.util.List;
import java.util.Optional;

public final class ActivityService {
    public record Activity(String id, String ownerId, String status, Instant scheduledAt) {}

    public static Optional<Activity> nearestPlanned(List<Activity> activities, String ownerId, Instant now) {
        return activities.stream().findFirst();
    }
}
