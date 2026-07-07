/**
 * v1 auth placeholder: a name typed by the staff member and persisted in
 * localStorage. This is NOT real authentication — there is no session, no
 * password, no server-side identity check. It exists only so approve/reject
 * actions and new tasks can be attributed to someone. Replace with real auth
 * before this app leaves v1.
 */

const STORAGE_KEY = 'backoffice.actingUser.v1';

export function getActingUser(): string {
  if (typeof window === 'undefined') return '';
  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? '';
  } catch {
    return '';
  }
}

export function setActingUser(name: string): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(STORAGE_KEY, name);
  } catch {
    /* localStorage unavailable (private browsing, etc.) — ignore */
  }
}
