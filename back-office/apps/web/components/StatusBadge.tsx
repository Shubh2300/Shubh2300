type StatusColor = 'amber' | 'green' | 'red' | 'violet' | 'neutral';

// Color is reserved strictly for status, per the house style: amber = pending
// / in progress, green = verified/success, red = failed/rejected,
// violet = blocked / needs human review. Everything else is neutral.
const STATUS_COLOR: Record<string, StatusColor> = {
  pending: 'amber',
  pending_approval: 'amber',
  proposed: 'neutral',
  approved: 'neutral',
  executing: 'amber',
  planning: 'amber',
  awaiting_approval: 'amber',
  verifying: 'amber',
  in_progress: 'amber',
  open: 'neutral',
  success: 'green',
  verified: 'green',
  done: 'green',
  failed: 'red',
  rejected: 'red',
  expired: 'red',
  blocked: 'violet',
  needs_human_review: 'violet',
  cancelled: 'neutral',
};

export default function StatusBadge({ status }: { status?: string }) {
  if (!status) {
    return <span className="badge badge-neutral">unknown</span>;
  }
  const key = status.toLowerCase();
  const color = STATUS_COLOR[key] ?? 'neutral';
  return <span className={`badge badge-${color}`}>{key.replace(/_/g, ' ')}</span>;
}
