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
  default: 'bg-slate-100 text-slate-700 border-slate-200',
  primary: 'bg-blue-50 text-blue-700 border-blue-200',
  success: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  warning: 'bg-amber-50 text-amber-700 border-amber-200',
  danger: 'bg-rose-50 text-rose-700 border-rose-200',
  neutral: 'bg-slate-50 text-slate-600 border-slate-200',
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
              ? 'bg-emerald-500'
              : variant === 'danger'
              ? 'bg-rose-500'
              : variant === 'warning'
              ? 'bg-amber-500'
              : variant === 'primary'
              ? 'bg-blue-500'
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
  let colorStyle = 'bg-slate-100 text-slate-700 border-slate-200';

  if (m === 'GET') {
    colorStyle = 'bg-blue-50 text-blue-700 border-blue-200';
  } else if (m === 'POST') {
    colorStyle = 'bg-emerald-50 text-emerald-700 border-emerald-200';
  } else if (m === 'PUT') {
    colorStyle = 'bg-amber-50 text-amber-700 border-amber-200';
  } else if (m === 'PATCH') {
    colorStyle = 'bg-purple-50 text-purple-700 border-purple-200';
  } else if (m === 'DELETE') {
    colorStyle = 'bg-rose-50 text-rose-700 border-rose-200';
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
          className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border bg-blue-50 text-blue-700 border-blue-200 ${className}`}
        >
          <span className="w-1.5 h-1.5 rounded-full bg-blue-500 animate-ping" />
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
