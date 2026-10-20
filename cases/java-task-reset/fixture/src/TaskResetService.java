public final class TaskResetService {
    public enum Status { NEW, RUNNING, FAILED, CANCELLED, DONE }
    public record Task(String id, String owner, Status status, int attempt,
                       String error, long version, String lastResetId, String payload) {}
    public record ResetCommand(String actor, boolean admin, long expectedVersion, String requestId) {}
    public static Task reset(Task task, ResetCommand command) {
        return new Task(task.id(), task.owner(), Status.NEW, 0, null,
                        task.version() + 1, command.requestId(), null);
    }
}
