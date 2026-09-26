import { listCategories, listJobs, listQuestions } from "@/lib/api/client";
import { Filters, type ReviewFilters } from "./Filters";
import { ReviewQueue } from "./ReviewQueue";

const PAGE_SIZE = 50;

function first(v: string | string[] | undefined): string | undefined {
  return Array.isArray(v) ? v[0] : v;
}

function parseFilters(sp: Record<string, string | string[] | undefined>): ReviewFilters {
  const difficulty = Number(first(sp.difficulty));
  return {
    category: first(sp.category) || undefined,
    difficulty: Number.isInteger(difficulty) && difficulty >= 1 && difficulty <= 5 ? difficulty : undefined,
    // Job rows on /generate and the dashboard link here as ?job=<id>.
    job: first(sp.job) || first(sp.job_id) || undefined,
  };
}

export default async function ReviewPage(props: PageProps<"/review">) {
  const sp = await props.searchParams;
  const filters = parseFilters(sp);
  const page = Math.max(1, Number(first(sp.page)) || 1);

  const [categories, jobs, questions] = await Promise.all([
    listCategories(),
    listJobs(50),
    listQuestions({
      status: "pending",
      category: filters.category,
      difficulty: filters.difficulty,
      job_id: filters.job,
      page,
      page_size: PAGE_SIZE,
    }),
  ]);

  // Keyed on the filters so a filter change resets the queue's local state
  // (position, edit draft, selection); a refresh with the same filters keeps it.
  const queueKey = JSON.stringify([filters, page]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-4">
        <h1 className="text-lg font-semibold">Review</h1>
        <Filters filters={filters} categories={categories} jobs={jobs} />
      </div>
      <ReviewQueue
        key={queueKey}
        initialItems={questions.items}
        total={questions.total}
        page={page}
        pageSize={PAGE_SIZE}
        categories={categories}
      />
    </div>
  );
}
