import React from 'react';
import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard,
  Layers,
  Sparkles,
  Cpu,
  GitBranch,
} from 'lucide-react';

export const Sidebar: React.FC = () => {
  const navItems = [
    {
      label: 'Dashboard',
      to: '/',
      icon: LayoutDashboard,
      end: true,
    },
    {
      label: 'Projects',
      to: '/projects',
      icon: Layers,
      end: false,
    },
  ];

  return (
    <aside className="w-64 bg-slate-900 border-r border-slate-800 flex flex-col shrink-0 select-none">
      {/* Brand Header */}
      <div className="h-16 px-5 border-b border-slate-800 flex items-center gap-3">
        <div className="w-9 h-9 rounded-lg bg-gradient-to-br from-indigo-500 to-indigo-700 flex items-center justify-center text-white shadow-md shadow-indigo-500/20">
          <Cpu className="w-5 h-5" />
        </div>
        <div className="flex flex-col">
          <span className="font-semibold text-sm tracking-tight text-slate-100 flex items-center gap-1.5">
            Agent Testing
            <Sparkles className="w-3.5 h-3.5 text-indigo-400" />
          </span>
          <span className="text-[11px] text-slate-400 font-mono">
            Autonomous Engine
          </span>
        </div>
      </div>

      {/* Main Navigation */}
      <div className="flex-1 py-6 px-3 space-y-1">
        <div className="px-3 pb-2 text-[11px] font-semibold tracking-wider text-slate-500 uppercase">
          Navigation
        </div>

        {navItems.map((item) => {
          const Icon = item.icon;
          return (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all ${
                  isActive
                    ? 'bg-indigo-600/15 text-indigo-300 border border-indigo-500/30'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
                }`
              }
            >
              <Icon className="w-4 h-4 shrink-0" />
              <span>{item.label}</span>
            </NavLink>
          );
        })}
      </div>

      {/* Pipeline Status Info Footer */}
      <div className="p-4 border-t border-slate-800 bg-slate-950/40">
        <div className="rounded-lg bg-slate-900/80 border border-slate-800/80 p-3 space-y-2">
          <div className="flex items-center justify-between text-xs font-medium text-slate-300">
            <span className="flex items-center gap-1.5">
              <GitBranch className="w-3.5 h-3.5 text-slate-400" />
              Pipeline Core
            </span>
            <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-indigo-950 text-indigo-400 border border-indigo-800/50">
              Active
            </span>
          </div>
          <p className="text-[11px] text-slate-400 leading-relaxed">
            LangGraph agent with semantic RAG and deterministic re-testing.
          </p>
        </div>
      </div>
    </aside>
  );
};
