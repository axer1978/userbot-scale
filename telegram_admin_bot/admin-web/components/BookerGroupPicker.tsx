"use client";

// "Pick a group…" beside booking.provider: the groups the client's account
// is in, asked from the running account (WhatsApp or Telegram). Picking one
// puts its id in the field; Save config makes it the booker, and from then
// on the booking requests go to that group and anyone in it can answer.

import { useState } from "react";
import { api, errorText } from "@/lib/api";

type BookingGroup = { id: string; name: string; members: number | null; admins_only: boolean };

export function BookerGroupPicker({ tenantId, onPick }: { tenantId: number; onPick: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const [groups, setGroups] = useState<BookingGroup[] | null>(null);
  const [error, setError] = useState("");
  const [picked, setPicked] = useState("");

  const load = async () => {
    setOpen(true);
    setGroups(null);
    setError("");
    try {
      setGroups((await api<{ groups: BookingGroup[] }>("GET", `/api/tenants/${tenantId}/booking-groups`)).groups);
    } catch (err) {
      setError(errorText(err));
    }
  };

  if (!open) {
    return (
      <div className="cfg-helper">
        <button type="button" className="btn small" onClick={load}
                title="Send the booking requests to a group this account is in">Pick a group…</button>
        {picked && <span className="muted">Picked “{picked}”. Save config to use it.</span>}
      </div>
    );
  }
  return (
    <div className="cfg-helper">
      {!groups && !error && <span className="muted">Asking the account for its groups…</span>}
      {error && <span className="err">{error}</span>}
      {groups && groups.length === 0 && <span className="muted">This account is in no group. Add it to the group first.</span>}
      {groups && groups.length > 0 && (
        <select defaultValue="" onChange={(ev) => {
          const group = groups.find((g) => g.id === ev.target.value);
          if (!group) return;
          onPick(group.id);
          setPicked(group.name || group.id);
          setOpen(false);
        }}>
          <option value="">The group that gets the booking requests…</option>
          {groups.map((g) => (
            <option key={g.id} value={g.id}>
              {g.name || g.id}{g.members ? ` · ${g.members} members` : ""}{g.admins_only ? " · only admins can write" : ""}
            </option>
          ))}
        </select>
      )}
      <button type="button" className="btn small" onClick={() => setOpen(false)}>Cancel</button>
    </div>
  );
}
