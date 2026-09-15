import { ReportTemplate } from '../../utils/reportTemplates';
interface TemplatePickerProps {
    open: boolean;
    onClose: () => void;
    onSelect: (template: ReportTemplate) => void;
    measureCount: number;
    hasTimeDimension: boolean;
    hasCategoricalDimension: boolean;
}
export default function TemplatePicker({ open, onClose, onSelect, measureCount, hasTimeDimension, hasCategoricalDimension, }: TemplatePickerProps): import("react/jsx-runtime").JSX.Element;
export {};
