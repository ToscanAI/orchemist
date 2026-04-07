import EditTemplateClient from './EditTemplateClient';

/**
 * Required for `output: 'export'` — generates a shell page at
 * `/templates/_/edit` which the SPA fallback serves for any `/templates/{id}/edit`.
 */
export async function generateStaticParams() {
  return [{ id: '_' }];
}

export default function EditTemplatePage() {
  return <EditTemplateClient />;
}
