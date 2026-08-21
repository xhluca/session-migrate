import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

/* The vendored asciinema stylesheet is a runtime asset shared with GitHub Pages. */

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

const origin = "https://session-migrate.github.io";
const title = "session-migrate — Carry the conversation forward";
const description = "Move coding agent sessions among Claude Code, Codex, Pi, OpenCode, Copilot, Antigravity, and Cursor.";
const image = `${origin}/og.png`;

export const metadata: Metadata = {
  metadataBase: new URL(`${origin}/`),
  title,
  description,
  alternates: { canonical: "/" },
  icons: { icon: "/logo-mark.svg", shortcut: "/logo-mark.svg" },
  openGraph: { title, description, type: "website", url: origin, images: [{ url: image, width: 1731, height: 909, alt: "session-migrate" }] },
  twitter: { card: "summary_large_image", title, description, images: [image] },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <head><link rel="stylesheet" href="/asciinema-player.css" /></head>
      <body className={`${geistSans.variable} ${geistMono.variable}`}>
        {children}
        <script src="/asciinema-player.min.js" defer />
      </body>
    </html>
  );
}
