import type { LoginFormData } from '../../hooks/useAuth';
interface LoginScreenProps {
    onLogin: (data: LoginFormData, remember: boolean) => Promise<void>;
    loading: boolean;
    error: string | null;
}
export default function LoginScreen({ onLogin, loading, error }: LoginScreenProps): import("react/jsx-runtime").JSX.Element;
export {};
