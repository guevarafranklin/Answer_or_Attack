import { listCategories } from "@/lib/api/client";
import { CategoryForm } from "./CategoryForm";
import { CategoryList } from "./CategoryList";

export default async function CategoriesPage() {
  const categories = await listCategories();

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <h1 className="text-lg font-semibold">Categories</h1>
        <CategoryList categories={categories} />
      </div>
      <section className="flex flex-col gap-2">
        <h2 className="font-semibold">New category</h2>
        <CategoryForm />
      </section>
    </div>
  );
}
