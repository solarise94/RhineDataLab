import { redirect } from "next/navigation";

// The global card library now lives inside the project workspace shell so the
// sidebar and project context are preserved. This bare route redirects to the
// project list as a fallback for legacy/deep links.
export default function CardLibraryRoute() {
  redirect("/projects");
}
