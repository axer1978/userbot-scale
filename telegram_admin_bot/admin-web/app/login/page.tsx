import type { Metadata } from "next";
import { LoginForm } from "./LoginForm";

export const metadata: Metadata = { title: "Sign in — Telegram AI Assistant" };

export default async function LoginPage({ searchParams }: PageProps<"/login">) {
  const { next } = await searchParams;
  // Only a path on this site: never send the browser off somewhere after sign-in.
  const target = typeof next === "string" && next.startsWith("/") && !next.startsWith("//") ? next : "/";
  return <LoginForm next={target} />;
}
