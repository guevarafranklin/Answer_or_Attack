"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

export function NavLink({ href, children }: { href: string; children: React.ReactNode }) {
  const pathname = usePathname();
  const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
  return (
    <Link
      href={href}
      className={`rounded px-2 py-1 ${active ? "bg-gray-900 text-white" : "text-gray-700 hover:bg-gray-100"}`}
    >
      {children}
    </Link>
  );
}
