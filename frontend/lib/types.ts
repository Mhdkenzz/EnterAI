export type User = {
  id: string;
  name: string;
  email: string;
  role: string;
  kind?: string;
  title?: string;
  avatar?: string;
  email_verified?: boolean;
};
export type Doc = { id: string; file_name: string };
export type Project = {
  id: string;
  name: string;
  code: string;
  description?: string;
  status?: string;
  health: string;
  color: string;
  progress: number;
  task_count: number;
  team?: string;
  due_date?: string;
  documents?: Doc[];
};
export type Task = {
  id: string;
  project_id: string;
  parent_id?: string | null;
  title: string;
  description?: string;
  status: string;
  priority: string;
  labels: string[];
  due_date?: string;
  assignee?: User;
};
export type InboxNotice = { id: string; title: string; body?: string; href?: string; read: boolean };
export type ActionDetail = { label: string; value: string };
export type Action = {
  details?: ActionDetail[];
  label: string;
  confirmation_token: string;
  requires_confirmation: boolean;
};
export type Agent = {
  id: string;
  name: string;
  title?: string;
  avatar?: string;
  hierarchy_level: string;
  parent_agent_id?: string | null;
  current_task?: Task | null;
  last_completed_task?: Task | null;
  is_working?: boolean;
  is_blocked?: boolean;
};
export type HierarchyConfigT = {
  vp_count: number;
  directors_per_vp: number;
  managers_per_director: number;
  workers_per_manager: number;
};
export type AgentMsg = { id: string; role: string; body: string; actions?: Action[] };
