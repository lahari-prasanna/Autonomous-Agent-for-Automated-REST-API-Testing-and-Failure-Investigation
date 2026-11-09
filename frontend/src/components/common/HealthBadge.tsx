import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../services/api';
import { AlertCircle } from 'lucide-react';

export const HealthBadge: React.FC<{ className?: string }> = ({ className = '' }) => {
  const { data, isError, isLoading } = useQuery({
    queryKey: ['system', 'health'],
    queryFn: api.checkHealth,
    refetchInterval: 30000, // 30 seconds
    retry: 1,
    staleTime: 15000,
  });

  if (isLoading) {
    return (
      <div
        className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-slate-100 border border-slate-200 text-xs text-slate-500 ${className}`}
        title="Checking backend service connectivity..."
      >
        <span className="w-1.5 h-1.5 rounded-full bg-slate-400 animate-pulse" />
        <span>Connecting...</span>
      </div>
    );
  }

  if (isError || !data || data.status !== 'ok') {
    return (
      <div
        className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-rose-50 border border-rose-200 text-xs text-rose-700 ${className}`}
        title="Unable to reach backend service"
      >
        <AlertCircle className="w-3.5 h-3.5 text-rose-500 shrink-0" />
        <span className="font-medium">Backend Offline</span>
      </div>
    );
  }

  return (
    <div
      className={`flex items-center gap-2 px-2.5 py-1 rounded-full bg-emerald-50/80 border border-emerald-200 text-xs text-emerald-800 ${className}`}
      title="Backend connected"
    >
      <span className="relative flex h-2 w-2">
        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
        <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
      </span>
      <span className="font-medium">Backend Online</span>
    </div>
  );
};
