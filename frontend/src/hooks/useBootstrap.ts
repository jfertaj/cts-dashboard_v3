// src/hooks/useExplorerBootstrap.ts
import useSWR from "swr";
import { explorerBootstrap, getExplorerFields } from "../lib/api";

async function fetcher() {
  const [boot, fieldsResp] = await Promise.all([
    explorerBootstrap(),
    getExplorerFields(),
  ]);

  const fields = Array.isArray(fieldsResp?.fields)
    ? fieldsResp.fields
    : Array.isArray(fieldsResp)
    ? fieldsResp
    : [];

  return { bootstrap: boot, fields };
}

export function useExplorerBootstrap() {
  const { data, error, isLoading, mutate } = useSWR(
    "explorer-bootstrap+fields", // 👈 clave cache única
    fetcher,
    {
      revalidateOnFocus: false,
      revalidateIfStale: false,
      revalidateOnReconnect: false,
    }
  );

  return {
    bootstrap: data?.bootstrap,
    fields: data?.fields || [],
    error,
    isLoading,
    refresh: mutate,
  };
}