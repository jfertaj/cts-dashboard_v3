import { useState, useEffect } from "react";

type Assignment = {
  id: string;
  name: string;
  stage?: string | null;
  type?: string | null;
  opportunity_id?: string | null;
  opportunity_name?: string | null;
  created?: string | null;
};

type AccountExtras = {
  account_id: string;
  member?: { account_id: string; name: string } | null;
  pi?: { contact_id: string; name: string; email?: string | null; phone?: string | null } | null;
  opportunity?: { id: string; name: string; new_dx_u18?: number | null; new_dx_o18?: number | null } | null;
  assignments?: Assignment[];
  csContribution?: Record<string, boolean | null>;
  newDxUnder18?: number | null;
  newDxOver18?: number | null;
};

const cache: Record<string, AccountExtras> = {};

export function useAccountExtras(accountId?: string) {
  const [extras, setExtras] = useState<AccountExtras | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    if (!accountId) return;
    if (cache[accountId]) {
      setExtras(cache[accountId]);
      return;
    }

    setLoading(true);
    fetch(`/api/salesforce/explorer/account-extras/${accountId}`, {
      credentials: "include", // importante: enviar cookie sf_session
    })
      .then(async (r) => {
        if (!r.ok) {
          throw new Error(`HTTP ${r.status}`);
        }
        return r.json();
      })
      .then((data) => {
        cache[accountId] = data;
        setExtras(data);
      })
      .catch((err) => setError(err))
      .finally(() => setLoading(false));
  }, [accountId]);

  return { extras, loading, error };
}