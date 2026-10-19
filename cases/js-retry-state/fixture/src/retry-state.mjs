export function canRetry(state) {
  return state.status !== 'pending';
}

export function reduceRetry(state, event) {
  if (event?.type === 'START') {
    return { ...state, status: 'pending', requestId: event.requestId, attempts: state.attempts + 1 };
  }
  if (event?.type === 'SUCCESS') return { ...state, status: 'succeeded' };
  if (event?.type === 'FAILURE') return { ...state, status: 'failed', error: event.error };
  return state;
}
