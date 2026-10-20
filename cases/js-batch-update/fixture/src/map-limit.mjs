export async function mapLimit(items, concurrency, worker) {
  return Promise.all(items.map((value, index) => worker(value, index)));
}
