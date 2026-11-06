import React from 'react';
import type { RunStatus } from '../../types/api';

export type BadgeVariant = 'default' | 'primary' | 'success' | 'warning' | 'danger' | 'neutral';

interface BadgeProps {
  children: React.ReactNode;
  variant?: BadgeVariant;
  className?: string;
  dot?: boolean;
}

const variantStyles: Record<BadgeVariant, string> = {
  default: 'bg-slate-800 text-slate-300 border-slate-700',
  primary: 'bg-indigo-950/60 text-indigo-300 border-indigo-700/50',
  success: 'bg-emerald-950/60 text-emerald-300 border-emerald-700/50',
  warning: 'bg-amber-950/60 text-amber-300 border-amber-700/50',
  danger: 'bg-rose-950/60 text-rose-300 border-rose-700/50',
  neutral: 'bg-slate-800/80 text-slate-400 border-slate-700/60',
};

export const Badge: React.FC<BadgeProps> = ({
  children,
  variant = 'default',
  className = '',
  dot = false,
}) => {
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border ${variantStyles[variant]} ${className}`}
    >
      {dot && (
        <span
          className={`w-1.5 h-1.5 rounded-full ${
            variant === 'success'
              ? 'bg-emerald-400'
              : variant === 'danger'
              ? 'bg-rose-400'
              : variant === 'warning'
              ? 'bg-amber-400'
              : variant === 'primary'
              ? 'bg-indigo-400'
              : 'bg-slate-400'
          }`}
        />
      )}
      {children}
    </span>
  );
};

export const HttpMethodBadge: React.FC<{ method: string; className?: string }> = ({
  method,
  className = '',
}) => {
  const m = method.toUpperCase();
  let colorStyle = 'bg-slate-800 text-slate-300 border-slate-700';

  if (m === 'GET') {
    colorStyle = 'bg-blue-950/70 text-blue-300 border-blue-700/50';
  } else if (m === 'POST') {
    colorStyle = 'bg-emerald-950/70 text-emerald-300 border-emerald-700/50';
  } else if (m === 'PUT') {
    colorStyle = 'bg-amber-950/70 text-amber-300 border-amber-700/50';
  } else if (m === 'PATCH') {
    colorStyle = 'bg-purple-950/70 text-purple-300 border-purple-700/50';
  } else if (m === 'DELETE') {
    colorStyle = 'bg-rose-950/70 text-rose-300 border-rose-700/50';
  }

  return (
    <span
      className={`inline-block font-mono text-xs font-semibold px-2 py-0.5 rounded border ${colorStyle} ${className}`}
    >
      {m}
    </span>
  );
};

export const StatusBadge: React.FC<{ status: RunStatus | string; className?: string }> = ({
  status,
  className = '',
}) => {
  const s = status.toLowerCase();

  switch (s) {
    case 'completed':
      return (
        <Badge variant="success" dot className={className}>
          Completed
        </Badge>
      );
    case 'running':
      return (
        <span
          className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border bg-blue-950/60 text-blue-300 border-blue-700/50 ${className}`}
        >
          <span className="w-1.5 h-1.5 rounded-full bg-blue-400 animate-ping" />
          Running
        </span>
      );
    case 'pending':
      return (
        <Badge variant="neutral" dot className={className}>
          Pending
        </Badge>
      );
    case 'failed':
      return (
        <Badge variant="danger" dot className={className}>
          Failed
        </Badge>
      );
    case 'cancelled':
      return (
        <Badge variant="warning" dot className={className}>
          Cancelled
        </Badge>
      );
    default:
      return (
        <Badge variant="default" className={className}>
          {status}
        </Badge>
      );
  }
};
