import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Answer or Attack — Admin",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full">
      <body className="min-h-full text-sm">{children}</body>
    </html>
  );
}
