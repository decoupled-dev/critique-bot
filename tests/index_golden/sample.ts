import { ttl } from './config';
import * as fs from 'fs';

export interface Entry {
  value: string;
}

export class Cache extends Store implements Lookup {
  get(key: string): Entry | undefined {
    if (this.expired(key)) {
      console.warn("cache entry expired");
      return undefined;
    }
    return this.read(key);
  }

  private expired(key: string): boolean {
    return Date.now() > ttl(key);
  }
}

export function makeCache(): Cache {
  return new Cache();
}
