import { LoginForm } from "./LoginForm";

export default async function LoginPage(props: PageProps<"/login">) {
  const { next } = await props.searchParams;
  return (
    <main className="mx-auto mt-24 w-80 rounded border border-gray-200 bg-white p-6">
      <h1 className="mb-4 text-base font-semibold">Answer or Attack — Admin</h1>
      <LoginForm next={typeof next === "string" ? next : "/"} />
    </main>
  );
}
