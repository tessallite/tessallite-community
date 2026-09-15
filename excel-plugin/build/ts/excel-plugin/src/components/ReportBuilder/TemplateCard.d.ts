import type { ReportTemplate } from '../../utils/reportTemplates';
interface TemplateCardProps {
    template: ReportTemplate;
    isApplicable: boolean;
    missingFields: string[];
    onSelect: (template: ReportTemplate) => void;
}
export default function TemplateCard({ template, isApplicable, missingFields, onSelect, }: TemplateCardProps): import("react/jsx-runtime").JSX.Element;
export {};
