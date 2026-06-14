import { ProjectWorkspace } from "@/components/layout/ProjectWorkspace";

export default async function GlobalCardLibraryProjectPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params;
  return <ProjectWorkspace projectId={projectId} view="global-card-library" />;
}
