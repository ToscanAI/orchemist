import TemplateDetailClient from './TemplateDetailClient';

export async function generateStaticParams() {
  return [{ id: '_' }];
}

export default function TemplateDetailPage() {
  return <TemplateDetailClient />;
}
