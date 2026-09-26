import { logout } from "@/app/login/actions";
import { NavLink } from "@/components/NavLink";

const NAV = [
  { href: "/", label: "Dashboard" },
  { href: "/generate", label: "Generate" },
  { href: "/review", label: "Review" },
  { href: "/health", label: "Health" },
  { href: "/categories", label: "Categories" },
] as const;

export default function PanelLayout({ children }: LayoutProps<"/">) {
  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex items-center gap-6 border-b border-gray-200 bg-white px-4 py-2">
        <span className="font-semibold">AoA Admin</span>
        <nav className="flex gap-1">
          {NAV.map((item) => (
            <NavLink key={item.href} href={item.href}>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <form action={logout} className="ml-auto">
          <button type="submit" className="text-gray-500 hover:text-gray-900">
            Log out
          </button>
        </form>
      </header>
      <main className="flex-1 p-4">{children}</main>
    </div>
  );
}
