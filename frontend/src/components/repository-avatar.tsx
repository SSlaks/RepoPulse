"use client";

import Image from "next/image";
import { useEffect, useState } from "react";

const FALLBACK_SOURCE = "/avatar-fallback.svg";
const RETRY_DELAYS_MS = [1_000, 2_000, 4_000, 8_000] as const;

interface RepositoryAvatarProps {
  owner: string;
  ownerGithubId?: number | null;
  size?: number;
}

export function RepositoryAvatar({ owner, ownerGithubId, size = 42 }: RepositoryAvatarProps) {
  if (!ownerGithubId) {
    return <AvatarImage owner={owner} size={size} source={FALLBACK_SOURCE} />;
  }
  return (
    <RetryingAvatar
      key={ownerGithubId}
      owner={owner}
      ownerGithubId={ownerGithubId}
      size={size}
    />
  );
}

function RetryingAvatar({
  owner,
  ownerGithubId,
  size,
}: {
  owner: string;
  ownerGithubId: number;
  size: number;
}) {
  const [retryAttempt, setRetryAttempt] = useState(0);
  const [source, setSource] = useState(() => buildAvatarSource(ownerGithubId, 0));

  useEffect(() => {
    if (source !== FALLBACK_SOURCE || retryAttempt >= RETRY_DELAYS_MS.length) {
      return;
    }
    const timeout = window.setTimeout(() => {
      const nextAttempt = retryAttempt + 1;
      setRetryAttempt(nextAttempt);
      setSource(buildAvatarSource(ownerGithubId, nextAttempt));
    }, RETRY_DELAYS_MS[retryAttempt]);
    return () => window.clearTimeout(timeout);
  }, [ownerGithubId, retryAttempt, source]);

  return <AvatarImage
    owner={owner}
    size={size}
    source={source}
    onError={() => {
      if (source !== FALLBACK_SOURCE) setSource(FALLBACK_SOURCE);
    }}
  />;
}

function AvatarImage({
  owner,
  size,
  source,
  onError,
}: {
  owner: string;
  size: number;
  source: string;
  onError?: () => void;
}) {
  return (
    <Image
      className="repo-avatar"
      src={source}
      alt={`${owner} 头像`}
      width={size}
      height={size}
      unoptimized
      onError={onError}
    />
  );
}

function buildAvatarSource(ownerGithubId: number, retryAttempt: number): string {
  const source = `/api/v1/avatars/${ownerGithubId}`;
  return retryAttempt ? `${source}?retry=${retryAttempt}` : source;
}
