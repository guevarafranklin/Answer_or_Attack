"use server";

import { redirect } from "next/navigation";
import { env } from "@/lib/env";
import { clearSessionCookie, passwordMatches, setSessionCookie } from "@/lib/session";

export interface LoginState {
  error: string | null;
}

/** Only same-origin paths may be used as a post-login destination. */
function safeNext(raw: FormDataEntryValue | null): string {
  const value = typeof raw === "string" ? raw : "";
  return value.startsWith("/") && !value.startsWith("//") ? value : "/";
}

export async function login(_prev: LoginState, formData: FormData): Promise<LoginState> {
  const password = formData.get("password");
  if (typeof password !== "string" || !(await passwordMatches(password, env.ADMIN_PASSWORD))) {
    return { error: "Wrong password." };
  }
  await setSessionCookie();
  redirect(safeNext(formData.get("next")));
}

export async function logout(): Promise<void> {
  await clearSessionCookie();
  redirect("/login");
}
