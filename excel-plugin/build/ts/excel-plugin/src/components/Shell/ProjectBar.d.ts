interface ProjectBarProps {
    projects: {
        id: string;
        name: string;
    }[];
    projectId: string | null;
    onProjectChange: (projectId: string) => void;
}
/**
 * The project row under the header: a selector when the user has more than
 * one project, otherwise the active project's name (or the loading copy).
 */
export default function ProjectBar({ projects, projectId, onProjectChange }: ProjectBarProps): import("react/jsx-runtime").JSX.Element;
export {};
