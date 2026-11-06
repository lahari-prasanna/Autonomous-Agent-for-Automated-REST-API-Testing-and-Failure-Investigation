import React from 'react';
import { Loader2 } from 'lucide-react';

export const Spinner: React.FC<{ size?: 'sm' | 'md' | 'lg'; className?: string }> = ({
  size = 'md',
  className = '',
}) => {
  const sizeMap = {
    sm: 'w-4 h-4',
    md: 'w-6 h-6',
    lg: 'w-8 h-8',
  };

  return (
    <Loader2
      className={`animate-spin text-indigo-400 ${sizeMap[size]} ${className}`}
    />
  );
};

export const Skeleton: React.FC<{ className?: string }> = ({ className = '' }) => {
  return <div className={`animate-pulse bg-slate-800/80 rounded ${className}`} />;
};

export const PageLoader: React.FC<{ message?: string }> = ({
  message = 'Loading...',
}) => {
  return (
    <div className="min-h-[300px] flex flex-col items-center justify-center p-8 text-center">
      <Spinner size="lg" className="mb-4" />
      <p className="text-sm text-slate-400 font-medium">{message}</p>
    </div>
  );
};

export interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}

export const EmptyState: React.FC<EmptyStateProps> = ({
  icon,
  title,
  description,
  action,
  className = '',
}) => {
  return (
    <div
      className={`flex flex-col items-center justify-center p-12 text-center rounded-xl border border-dashed border-slate-800 bg-slate-900/30 ${className}`}
    >
      {icon && <div className="text-slate-500 mb-3">{icon}</div>}
      <h4 className="text-base font-semibold text-slate-200 mb-1">{title}</h4>
      {description && (
        <p className="text-sm text-slate-400 max-w-sm mb-5">{description}</p>
      )}
      {action && <div>{action}</div>}
    </div>
  );
};
