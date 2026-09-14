import { translationRecordSchema, type TranslationRecord } from "./contracts";

export const README_TRANSLATION_DB_NAME = "repopulse-readme-ai";
export const README_TRANSLATION_STORE = "translations";
export const README_TRANSLATION_DB_VERSION = 1;

function openDatabase(): Promise<IDBDatabase> {
  if (typeof indexedDB === "undefined") {
    return Promise.reject(new Error("当前浏览器不支持翻译记录存储。"));
  }
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(README_TRANSLATION_DB_NAME, README_TRANSLATION_DB_VERSION);
    let settled = false;
    let blockedTimer: ReturnType<typeof setTimeout> | undefined;
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(README_TRANSLATION_STORE)) {
        database.createObjectStore(README_TRANSLATION_STORE, { keyPath: "repository" });
      }
    };
    request.onsuccess = () => {
      if (blockedTimer) clearTimeout(blockedTimer);
      if (settled) request.result.close();
      else { settled = true; resolve(request.result); }
    };
    request.onerror = () => {
      if (!settled) { settled = true; reject(request.error ?? new Error("无法打开翻译记录存储。")); }
    };
    request.onblocked = () => {
      blockedTimer = setTimeout(() => {
        if (!settled) { settled = true; reject(new Error("翻译记录存储被其他页面占用，请关闭其他项目页面后重试。")); }
      }, 5000);
    };
  });
}

function closeAfter<T>(database: IDBDatabase, task: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(README_TRANSLATION_STORE, "readonly");
    const request = task(transaction.objectStore(README_TRANSLATION_STORE));
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("无法读取翻译记录。"));
    transaction.onerror = () => reject(transaction.error ?? new Error("无法读取翻译记录。"));
    transaction.oncomplete = () => database.close();
    transaction.onabort = () => reject(transaction.error ?? new Error("无法读取翻译记录。"));
  });
}

export async function getReadmeTranslation(repository: string): Promise<TranslationRecord | null> {
  const database = await openDatabase();
  try {
    const value = await closeAfter(database, (store) => store.get(repository));
    if (value === undefined) return null;
    const parsed = translationRecordSchema.safeParse(value);
    if (!parsed.success) throw new Error("已保存的翻译记录格式无效，请重新翻译。");
    return parsed.data;
  } catch (error) {
    database.close();
    throw error;
  }
}

export async function saveReadmeTranslation(record: TranslationRecord, signal?: AbortSignal): Promise<void> {
  const parsed = translationRecordSchema.safeParse(record);
  if (!parsed.success) throw new Error("翻译记录格式无效，无法保存。");
  signal?.throwIfAborted();
  const database = await openDatabase();
  try {
    signal?.throwIfAborted();
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(README_TRANSLATION_STORE, "readwrite");
      const request = transaction.objectStore(README_TRANSLATION_STORE).put(parsed.data);
      let settled = false;
      const finish = (callback: () => void) => {
        if (settled) return;
        settled = true;
        signal?.removeEventListener("abort", abortTransaction);
        callback();
      };
      const abortError = () => signal?.reason instanceof Error
        ? signal.reason
        : new DOMException("翻译记录保存已取消。", "AbortError");
      const abortTransaction = () => {
        try { transaction.abort(); }
        catch { finish(() => reject(abortError())); }
      };
      request.onerror = () => finish(() => reject(request.error ?? new Error("无法保存翻译记录。")));
      transaction.oncomplete = () => finish(resolve);
      transaction.onerror = () => finish(() => reject(transaction.error ?? new Error("无法保存翻译记录。")));
      transaction.onabort = () => finish(() => reject(signal?.aborted ? abortError() : transaction.error ?? new Error("无法保存翻译记录。")));
      if (signal?.aborted) abortTransaction();
      else signal?.addEventListener("abort", abortTransaction, { once: true });
    });
  } finally {
    database.close();
  }
}

export async function fingerprintMarkdown(markdown: string): Promise<string> {
  const bytes = new TextEncoder().encode(markdown);
  if (globalThis.crypto?.subtle) {
    try {
      const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
      return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
    } catch {
      // Some restricted browser contexts expose SubtleCrypto but reject digest operations.
    }
  }
  return sha256(bytes);
}

function sha256(bytes: Uint8Array): string {
  const constants = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ];
  const state = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
  const paddedLength = Math.ceil((bytes.length + 9) / 64) * 64;
  const padded = new Uint8Array(paddedLength);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const view = new DataView(padded.buffer);
  const bitLength = bytes.length * 8;
  view.setUint32(paddedLength - 8, Math.floor(bitLength / 0x100000000), false);
  view.setUint32(paddedLength - 4, bitLength >>> 0, false);
  const schedule = new Uint32Array(64);
  for (let offset = 0; offset < padded.length; offset += 64) {
    for (let index = 0; index < 16; index++) schedule[index] = view.getUint32(offset + index * 4, false);
    for (let index = 16; index < 64; index++) {
      const s0 = rotateRight(schedule[index - 15], 7) ^ rotateRight(schedule[index - 15], 18) ^ (schedule[index - 15] >>> 3);
      const s1 = rotateRight(schedule[index - 2], 17) ^ rotateRight(schedule[index - 2], 19) ^ (schedule[index - 2] >>> 10);
      schedule[index] = (schedule[index - 16] + s0 + schedule[index - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = state;
    for (let index = 0; index < 64; index++) {
      const sum1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
      const choice = (e & f) ^ (~e & g);
      const temp1 = (h + sum1 + choice + constants[index] + schedule[index]) >>> 0;
      const sum0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (sum0 + majority) >>> 0;
      [h, g, f, e, d, c, b, a] = [g, f, e, (d + temp1) >>> 0, c, b, a, (temp1 + temp2) >>> 0];
    }
    state[0] = (state[0] + a) >>> 0; state[1] = (state[1] + b) >>> 0;
    state[2] = (state[2] + c) >>> 0; state[3] = (state[3] + d) >>> 0;
    state[4] = (state[4] + e) >>> 0; state[5] = (state[5] + f) >>> 0;
    state[6] = (state[6] + g) >>> 0; state[7] = (state[7] + h) >>> 0;
  }
  return state.map((value) => value.toString(16).padStart(8, "0")).join("");
}

function rotateRight(value: number, bits: number): number {
  return (value >>> bits) | (value << (32 - bits));
}
