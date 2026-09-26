import Link from "next/link";
import { notFound } from "next/navigation";
import { ApiError, getQuestion, listCategories } from "@/lib/api/client";
import { QuestionEditor } from "./QuestionEditor";

/** Edit any question by id, whatever its status (Health → Edit lands here). Not in the nav. */
export default async function QuestionPage(props: PageProps<"/questions/[id]">) {
  const { id } = await props.params;
  let question;
  try {
    question = await getQuestion(id);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) notFound();
    throw err;
  }
  const categories = await listCategories();
  const category = categories.find((c) => c.id === question.category_id);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-baseline gap-3">
        <h1 className="text-lg font-semibold">Question</h1>
        <span className="text-gray-500">
          {category?.slug ?? question.category_id} · {question.status}
        </span>
        <Link href="/health" className="ml-auto text-sm underline">
          ← health
        </Link>
      </div>
      <QuestionEditor key={question.updated_at} initial={question} />
    </div>
  );
}
