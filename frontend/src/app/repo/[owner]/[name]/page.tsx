import type { Metadata } from "next";
import {
  ArrowLeft,
  ArrowUpRight,
  CalendarDays,
  CircleDot,
  Code2,
  GitFork,
  Scale,
  Star,
} from "lucide-react";
import Link from "next/link";
import { headers } from "next/headers";
import { notFound } from "next/navigation";
import { Suspense } from "react";

import { RepositoryAvatar } from "@/components/repository-avatar";
import { RepositoryReadme, RepositoryReadmeFallback } from "@/components/repository-readme";
import { StarChart } from "@/components/star-chart";
import { fetchRepository, fetchSnapshots } from "@/lib/api";
import { formatDate, formatNumber } from "@/lib/format";
import { serializeJsonLd } from "@/lib/json-ld";
import { getRepositoryDescription } from "@/lib/repository-copy";

export const dynamic = "force-dynamic";

interface RepositoryPageProps {
  params: Promise<{ owner: string; name: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export async function generateMetadata({ params }: RepositoryPageProps): Promise<Metadata> {
  const { owner, name } = await params;
  try {
    const repository = await fetchRepository(owner, name);
    const description = getRepositoryDescription(repository);
    return {
      title: repository.name,
      description: description ?? `查看 ${repository.name} 的 Star 增长趋势。`,
      alternates: { canonical: `/repo/${owner}/${name}` },
      openGraph: {
        title: `${repository.name} · RepoPulse`,
        description: description ?? "GitHub 项目 Star 增长趋势",
      },
    };
  } catch {
    return { title: name };
  }
}

export default async function RepositoryPage({ params, searchParams }: RepositoryPageProps) {
  const { owner, name } = await params;
  const nonce = (await headers()).get("x-nonce") ?? undefined;
  const query = await searchParams;
  const returnTo = safeReturnTo(firstValue(query.returnTo));
  const [repositoryResult, snapshotResult] = await Promise.allSettled([
    fetchRepository(owner, name),
    fetchSnapshots(owner, name, "90d"),
  ]);
  if (repositoryResult.status === "rejected") notFound();

  const repository = repositoryResult.value;
  const description = getRepositoryDescription(repository);
  const snapshots = snapshotResult.status === "fulfilled" ? snapshotResult.value.data : [];
  const schema = {
    "@context": "https://schema.org",
    "@type": "SoftwareSourceCode",
    name: repository.name,
    description,
    codeRepository: repository.html_url,
    programmingLanguage: repository.language,
    license: repository.license_name,
  };

  return (
    <main className="repo-page">
      <section className="repo-identity">
        <div className="shell">
          <Link className="back-link" href={returnTo}>
            <ArrowLeft size={16} /> 返回增长榜
          </Link>
          <div className="repo-title-row">
            <RepositoryAvatar
              owner={repository.owner}
              ownerGithubId={repository.owner_github_id}
              size={58}
            />
            <div className="repo-title">
              <span>{repository.owner}</span>
              <h1>{repository.name}</h1>
            </div>
            <a
              className="primary-button"
              href={repository.html_url}
              target="_blank"
              rel="noreferrer"
            >
              在 GitHub 查看 <ArrowUpRight size={17} />
            </a>
          </div>
          <p className="repo-description">{description}</p>
          <div className="topic-row">
            {repository.topics.map((topic) => <span key={topic}>{topic}</span>)}
          </div>
        </div>
      </section>

      <section className="repo-data shell">
        <div className="metric-band">
          <Metric icon={<Star size={18} />} label="Star" value={formatNumber(repository.stars_count)} />
          <Metric icon={<GitFork size={18} />} label="Fork" value={formatNumber(repository.forks_count)} />
          <Metric icon={<CircleDot size={18} />} label="开放 Issue" value={formatNumber(repository.open_issues_count)} />
          <Metric icon={<Code2 size={18} />} label="主要语言" value={repository.language ?? "未知"} />
        </div>

        <StarChart owner={owner} name={name} initialData={snapshots} />

        <Suspense fallback={<RepositoryReadmeFallback />}>
          <RepositoryReadme owner={owner} name={name} />
        </Suspense>

        <div className="repo-metadata">
          <div className="section-heading"><div><span>项目档案</span><h2>仓库信息</h2></div><p>数据来自公开的 GitHub 仓库元数据。</p></div>
          <dl>
            <div><dt><Scale size={16} />开源许可证</dt><dd>{repository.license_name ?? "未声明"}</dd></div>
            <div><dt><CalendarDays size={16} />首次创建</dt><dd>{formatDate(repository.github_created_at)}</dd></div>
            <div><dt><CalendarDays size={16} />最近推送</dt><dd>{formatDate(repository.pushed_at, true)}</dd></div>
            <div><dt><CalendarDays size={16} />开始追踪</dt><dd>{formatDate(repository.first_tracked_at)}</dd></div>
          </dl>
        </div>
      </section>
      <script
        nonce={nonce}
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: serializeJsonLd(schema) }}
      />
    </main>
  );
}

function Metric({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return <div className="metric"><span>{icon}</span><div><small>{label}</small><strong>{value}</strong></div></div>;
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function safeReturnTo(value: string | undefined): string {
  if (!value || value.length > 2048 || value.includes("\\")) return "/ranking?period=7";
  return value.startsWith("/") && !value.startsWith("//") ? value : "/ranking?period=7";
}
