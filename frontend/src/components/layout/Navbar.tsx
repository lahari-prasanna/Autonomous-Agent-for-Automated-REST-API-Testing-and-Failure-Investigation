import React from 'react';
import { useLocation } from 'react-router-dom';
import { HealthBadge } from '../common/HealthBadge';

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
    <header className="h-16 border-b border-slate-200 bg-white/90 backdrop-blur-md px-6 flex items-center justify-between sticky top-0 z-30">
      <div className="flex items-center gap-3">
        <div className="flex flex-col">
          <span className="text-[11px] font-semibold tracking-wider text-slate-400 uppercase">
            {section}
          </span>
          <h1 className="text-lg font-semibold text-slate-900 tracking-tight">
            {title}
          </h1>
        </div>
      </div>

      <div className="flex items-center gap-3">
        <HealthBadge />
      </div>
    </header>
  );
};
