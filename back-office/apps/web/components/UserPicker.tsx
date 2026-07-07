'use client';

import { useEffect, useState } from 'react';
import { getActingUser, setActingUser } from '@/lib/user';

/**
 * v1 auth widget: lets a staff member declare who they are, persisted to
 * localStorage. Used to attribute new tasks and approve/reject decisions.
 * Deliberately unstyled as "real" auth — labeled so nobody mistakes it for
 * one.
 */
export default function UserPicker() {
  const [name, setName] = useState('');
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    const current = getActingUser();
    setName(current);
    setDraft(current);
    setEditing(!current);
  }, []);

  function save() {
    const trimmed = draft.trim();
    setActingUser(trimmed);
    setName(trimmed);
    setEditing(false);
  }

  return (
    <div className="user-picker">
      <div className="user-picker-label">Acting as (v1 auth)</div>
      {editing ? (
        <form
          className="user-picker-edit"
          onSubmit={(e) => {
            e.preventDefault();
            save();
          }}
        >
          <input
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Your name"
          />
          <button type="submit" disabled={!draft.trim()}>
            Save
          </button>
        </form>
      ) : (
        <button
          type="button"
          className="user-picker-current"
          onClick={() => setEditing(true)}
        >
          {name || 'Set your name'}
        </button>
      )}
    </div>
  );
}
