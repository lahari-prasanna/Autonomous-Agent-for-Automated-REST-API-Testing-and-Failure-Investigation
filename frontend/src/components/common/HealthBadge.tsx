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
        className={`flex items-center gap-2 px-3 py-1.5 rounded-full bg-slate-900 border border-slate-800 text-xs text-slate-400 ${className}`}
        title="Checking backend service connectivity..."
      >
        <span className="w-2 h-2 rounded-full bg-slate-500 animate-pulse" />
        <span>Connecting...</span>
      </div>
    );
  }

  if (isError || !data || data.status !== 'ok') {
    return (
      <div
        className={`flex items-center gap-2 px-3 py-1.5 rounded-full bg-rose-950/40 border border-rose-800/60 text-xs text-rose-300 ${className}`}
        title="Unable to reach FastAPI backend on /health"
      >
        <AlertCircle className="w-3.5 h-3.5 text-rose-400 shrink-0" />
        <span className="font-medium">Backend Offline</span>
      </div>
    );
  }

  return (
    <div
      className={`flex items-center gap-2 px-3 py-1.5 rounded-full bg-emerald-950/40 border border-emerald-800/50 text-xs text-emerald-300 ${className}`}
      title={`Backend connected | Env: ${data.environment} | Checked: ${new Date(
        data.timestamp
      ).toLocaleTimeString()}`}
    >
      <span className="relative flex h-2 w-2">
        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
        <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
      </span>
      <span className="font-medium">Backend Online</span>
      {data.environment && (
        <span className="font-mono text-[10px] px-1.5 py-0.2 rounded bg-emerald-900/50 text-emerald-300 uppercase tracking-wider">
          {data.environment}
        </span>
      )}
    </div>
  );
};
