'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import UserPicker from './UserPicker';

const LINKS = [
  { href: '/', label: 'Dashboard' },
  { href: '/tasks', label: 'Tasks' },
  { href: '/approvals', label: 'Approvals' },
  { href: '/runs', label: 'Runs' },
  { href: '/audit', label: 'Audit' },
  { href: '/patients', label: 'Patients' },
];

export default function Nav() {
  const pathname = usePathname();

  return (
    <nav className="sidebar">
      <div className="sidebar-brand">
        <span className="sidebar-brand-mark">BO</span>
        <span>Back Office</span>
      </div>
      <ul className="sidebar-links">
        {LINKS.map((link) => {
          const active =
            link.href === '/' ? pathname === '/' : pathname?.startsWith(link.href);
          return (
            <li key={link.href}>
              <Link href={link.href} className={active ? 'active' : undefined}>
                {link.label}
              </Link>
            </li>
          );
        })}
      </ul>
      <div className="sidebar-footer">
        <UserPicker />
      </div>
    </nav>
  );
}
