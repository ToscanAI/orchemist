import RunDetailClient from './RunDetailClient';

export async function generateStaticParams() {
  return [{ id: '_' }];
}

export default function RunDetailPage() {
  return <RunDetailClient />;
}
