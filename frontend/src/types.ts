export interface ResponseRecord {
  site: string;
  version: string;
  type: string;
  section: string;
  question: string;
  answer: string;
  subsection?: string;
  latitude?: number;
  longitude?: number;
  city?: string;
  country?: string;
}

export type Tab = "upload" | "explorer" | "members" | "chat" | "assignments";