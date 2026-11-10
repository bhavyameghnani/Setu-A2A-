// A department slug is lowercase letters, digits and hyphens, starting with a letter or digit
// (server/app/schemas/core.py::DepartmentCreate). Derive one from the name as the person types.
export function slugify(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "") // é -> e
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export const validSlug = (s: string) => /^[a-z0-9][a-z0-9-]*$/.test(s);
