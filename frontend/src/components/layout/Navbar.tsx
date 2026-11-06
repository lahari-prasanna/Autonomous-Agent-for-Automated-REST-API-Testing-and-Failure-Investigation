import React from 'react';
import { useLocation } from 'react-router-dom';
import { HealthBadge } from '../common/HealthBadge';
import { ShieldCheck, Terminal } from 'lucide-react';

interface NavbarProps {
  onToggleSidebar?: () => void;
}

export const Navbar: React.FC<NavbarProps> = () => {
  const location = useLocation();

  // Generate readable title from path
  const getPageInfo = (path: string) => {
    if (path === '/') return { title: 'Dashboard', section: 'Overview' };
    if (path === '/projects') return { title: 'Projects', section: 'Specifications' };
    if (path.startsWith('/projects/')) return { title: 'Project Details', section: 'Projects' };
    if (path.includes('/report')) return { title: 'Diagnostic Report', section: 'Runs' };
    if (path.startsWith('/runs/')) return { title: 'Live Test Run', section: 'Execution' };
    return { title: 'API Testing Agent', section: 'Platform' };
  };

  const { title, section } = getPageInfo(location.pathname);

  return (
    <header className="h-16 border-b border-slate-800 bg-slate-900/60 backdrop-blur-md px-6 flex items-center justify-between sticky top-0 z-30">
      <div className="flex items-center gap-3">
        <div className="flex flex-col">
          <span className="text-xs font-semibold tracking-wider text-slate-500 uppercase">
            {section}
          </span>
          <h1 className="text-lg font-semibold text-slate-100 tracking-tight">
            {title}
          </h1>
        </div>
      </div>

      <div className="flex items-center gap-4">
        <div className="hidden sm:flex items-center gap-2 text-xs text-slate-400 bg-slate-900 border border-slate-800 px-3 py-1.5 rounded-lg">
          <Terminal className="w-3.5 h-3.5 text-indigo-400" />
          <span className="font-mono">v1.0.0</span>
          <span className="text-slate-600">|</span>
          <span className="flex items-center gap-1 text-slate-300">
            <ShieldCheck className="w-3.5 h-3.5 text-emerald-400" />
            Sanitized
          </span>
        </div>
        <HealthBadge />
      </div>
    </header>
  );
};
