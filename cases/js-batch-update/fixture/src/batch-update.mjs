import {mapLimit} from './map-limit.mjs';

export async function updateSelected(ids, concurrency, update) {
  const values = await mapLimit(ids, concurrency, update);
  return ids.map((id, index) => ({id, status: 'fulfilled', value: values[index]}));
}
