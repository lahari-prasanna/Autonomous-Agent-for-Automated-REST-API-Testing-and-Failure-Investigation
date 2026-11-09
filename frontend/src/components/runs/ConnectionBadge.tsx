import React from 'react';
import type { SSEConnectionState } from '../../types/events';
import { WifiOff, CheckCircle2, AlertTriangle, RefreshCw } from 'lucide-react';

interface ConnectionBadgeProps {
  state: SSEConnectionState;
  className?: string;
}

export const ConnectionBadge: React.FC<ConnectionBadgeProps> = ({ state, className = '' }) => {
  switch (state) {
    case 'connected':
      return (
        <div
          className={`flex items-center gap-2 px-3 py-1.5 rounded-full bg-emerald-50 border border-emerald-200 text-xs text-emerald-800 ${className}`}
          title="Connected to real-time Server-Sent Events (SSE) progress stream"
        >
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
            <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
          </span>
          <span className="font-medium">Live SSE Connected</span>
        </div>
      );

    case 'connecting':
      return (
        <div
          className={`flex items-center gap-2 px-3 py-1.5 rounded-full bg-blue-50 border border-blue-200 text-xs text-blue-700 ${className}`}
          title="Establishing connection to Server-Sent Events stream..."
        >
          <RefreshCw className="w-3.5 h-3.5 animate-spin text-blue-600 shrink-0" />
          <span className="font-medium">Connecting...</span>
        </div>
      );

    case 'reconnecting':
      return (
        <div
          className={`flex items-center gap-2 px-3 py-1.5 rounded-full bg-amber-50 border border-amber-200 text-xs text-amber-800 ${className}`}
          title="Connection interrupted. Reconnecting with Last-Event-ID replay..."
        >
          <AlertTriangle className="w-3.5 h-3.5 animate-bounce text-amber-600 shrink-0" />
          <span className="font-medium">Reconnecting...</span>
        </div>
      );

    case 'completed':
      return (
        <div
          className={`flex items-center gap-2 px-3 py-1.5 rounded-full bg-slate-100 border border-slate-200 text-xs text-slate-700 ${className}`}
          title="Run execution stream has terminated"
        >
          <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600 shrink-0" />
          <span className="font-medium">Stream Finished</span>
        </div>
      );

    case 'disconnected':
    default:
      return (
        <div
          className={`flex items-center gap-2 px-3 py-1.5 rounded-full bg-slate-100 border border-slate-200 text-xs text-slate-500 ${className}`}
          title="SSE stream disconnected"
        >
          <WifiOff className="w-3.5 h-3.5 shrink-0" />
          <span className="font-medium">Disconnected</span>
        </div>
      );
  }
};
