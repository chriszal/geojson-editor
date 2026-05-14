import path from "node:path";
export const dataDir = process.env.DATA_DIR || path.join(process.cwd(), "data_new");
export const versionsDir = path.join(dataDir, "versions");
export const currentPath = path.join(dataDir, "current.json");