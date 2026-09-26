import { listCategories, listJobs } from "@/lib/api/client";
import { GenerateForm } from "./GenerateForm";
import { JobList } from "./JobList";

export default async function GeneratePage() {
  const [categories, jobs] = await Promise.all([listCategories(), listJobs(50)]);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-3">
        <h1 className="text-lg font-semibold">Generate</h1>
        <GenerateForm categories={categories} />
      </div>
      <section className="flex flex-col gap-2">
        <h2 className="font-semibold">Jobs</h2>
        <JobList initialJobs={jobs} />
      </section>
    </div>
  );
}
