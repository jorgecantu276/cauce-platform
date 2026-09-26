type StoredOperation<T extends object> = T & { createdAt: string };

function safeRead<T extends object>(storageKey: string): StoredOperation<T> | null {
  try {
    const raw = window.sessionStorage.getItem(storageKey);
    return raw ? JSON.parse(raw) as StoredOperation<T> : null;
  } catch {
    return null;
  }
}

function safeWrite<T extends object>(storageKey: string, value: StoredOperation<T>): void {
  try {
    window.sessionStorage.setItem(storageKey, JSON.stringify(value));
  } catch {
    // Idempotency still works for this tab even when session storage is unavailable.
  }
}

/**
 * Generates safe backend idempotency keys. Values are persisted only for an
 * in-flight intentional operation; no authentication material is stored.
 */
export function getOrCreateOperation<T extends object>(name: string, draftId: string, create: () => T): StoredOperation<T> {
  const storageKey = `canonical-operation:${name}:${draftId}`;
  const existing = safeRead<T>(storageKey);
  if (existing) return existing;
  const value = { ...create(), createdAt: new Date().toISOString() };
  safeWrite(storageKey, value);
  return value;
}

export function clearOperation(name: string, draftId: string): void {
  try {
    window.sessionStorage.removeItem(`canonical-operation:${name}:${draftId}`);
  } catch {
    // No persistence layer to clear.
  }
}

export function newKey(prefix: string): string {
  return `${prefix}:${crypto.randomUUID()}`;
}
