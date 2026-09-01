import {
  CustomerAction,
  CustomerPresentation,
  ChoicePresentation,
  StatusPresentation,
  ActionPresentation,
  HandoffPresentation,
} from "../api";

interface PresentationRendererProps {
  presentation: CustomerPresentation;
  onAction: (action: CustomerAction) => void;
  disabled?: boolean;
  choiceEnabled?: boolean;
}

function DisplayFields({ fields }: { fields: Array<{ label: string; value: string }> }) {
  if (fields.length === 0) return null;
  return <dl className="presentation-fields">{fields.map((field) => <div key={`${field.label}-${field.value}`}><dt>{field.label}</dt><dd>{field.value}</dd></div>)}</dl>;
}

export function ChoiceCard({ presentation, onAction, disabled = false }: {
  presentation: ChoicePresentation;
  onAction: (action: CustomerAction) => void;
  disabled?: boolean;
}) {
  return <section className="customer-presentation presentation-choice" aria-label={presentation.title}>
    <h3>{presentation.title}</h3>
    {presentation.description && <p className="presentation-description">{presentation.description}</p>}
    <div className="choice-options">{presentation.options.map((option) => <article className="choice-option" key={option.id}>
      <div className="choice-option-copy"><strong>{option.subject.title}</strong>{option.subject.subtitle && <small>{option.subject.subtitle}</small>}<DisplayFields fields={option.meta} /></div>
      <button type="button" onClick={() => onAction(option.action)} disabled={disabled}>{option.action.label}</button>
    </article>)}</div>
  </section>;
}

export function StatusCard({ presentation }: { presentation: StatusPresentation }) {
  return <section className="customer-presentation presentation-status" aria-label={presentation.title}>
    <div className="presentation-heading"><div><p className="presentation-kicker">{presentation.title}</p><strong>{presentation.subject.title}</strong>{presentation.subject.subtitle && <small>{presentation.subject.subtitle}</small>}</div><span className="presentation-status-value">{presentation.status}</span></div>
    <DisplayFields fields={presentation.details} />
  </section>;
}

function ActionButtons({ actions, onAction, disabled }: { actions: CustomerAction[]; onAction: (action: CustomerAction) => void; disabled: boolean }) {
  if (actions.length === 0) return null;
  return <div className="presentation-actions">{actions.slice(0, 2).map((action) => <button type="button" key={action.id} onClick={() => onAction(action)} disabled={disabled}>{action.label}</button>)}</div>;
}

export function ActionCard({ presentation, onAction, disabled = false }: {
  presentation: ActionPresentation;
  onAction: (action: CustomerAction) => void;
  disabled?: boolean;
}) {
  return <section className="customer-presentation presentation-action" aria-label={presentation.title}>
    <h3>{presentation.title}</h3>
    {presentation.description && <p className="presentation-description">{presentation.description}</p>}
    <ActionButtons actions={presentation.actions} onAction={onAction} disabled={disabled} />
  </section>;
}

export function HandoffCard({ presentation, onAction, disabled = false }: {
  presentation: HandoffPresentation;
  onAction: (action: CustomerAction) => void;
  disabled?: boolean;
}) {
  return <section className="customer-presentation presentation-handoff" aria-label={presentation.title}>
    <h3>{presentation.title}</h3>
    <p className="presentation-description">{presentation.description}</p>
    <ActionButtons actions={presentation.actions} onAction={onAction} disabled={disabled} />
  </section>;
}

export function PresentationRenderer({
  presentation,
  onAction,
  disabled = false,
  choiceEnabled = true,
}: PresentationRendererProps) {
  switch (presentation.kind) {
    case "choice": return <ChoiceCard presentation={presentation} onAction={onAction} disabled={disabled || !choiceEnabled} />;
    case "status": return <StatusCard presentation={presentation} />;
    case "action": return <ActionCard presentation={presentation} onAction={onAction} disabled={disabled} />;
    case "handoff": return <HandoffCard presentation={presentation} onAction={onAction} disabled={disabled} />;
    default: return null;
  }
}
