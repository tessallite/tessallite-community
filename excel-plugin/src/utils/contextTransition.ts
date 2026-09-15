/**
 * Serialises governed project/model transitions in the task pane.
 *
 * A generation is both the callback authority and the cancellation boundary:
 * beginning a newer transition aborts the older fetch and makes every older
 * state/storage/cache/recalculation continuation obsolete. The queue keeps
 * OfficeRuntime writes and workbook recalculations in order when an older
 * operation was already in flight when the newer transition began.
 */
export interface ContextTransition {
  readonly generation: number;
  readonly signal: AbortSignal;
  isCurrent: () => boolean;
}

export interface ContextTransitionCoordinator {
  begin: () => ContextTransition;
  currentGeneration: () => number;
  isCurrent: (generation: number) => boolean;
  run: <T>(
    transition: ContextTransition,
    work: (transition: ContextTransition) => Promise<T>,
  ) => Promise<T | undefined>;
  cancel: () => void;
}

export function createContextTransitionCoordinator(): ContextTransitionCoordinator {
  let generation = 0;
  let active: { generation: number; controller: AbortController } | null = null;
  let queue: Promise<void> = Promise.resolve();

  const isCurrent = (candidate: number): boolean =>
    active?.generation === candidate && !active.controller.signal.aborted;

  const begin = (): ContextTransition => {
    active?.controller.abort();
    const nextGeneration = generation + 1;
    generation = nextGeneration;
    const controller = new AbortController();
    active = { generation: nextGeneration, controller };
    return {
      generation: nextGeneration,
      signal: controller.signal,
      isCurrent: () => isCurrent(nextGeneration),
    };
  };

  const run = <T>(
    transition: ContextTransition,
    work: (current: ContextTransition) => Promise<T>,
  ): Promise<T | undefined> => {
    const task = queue.then(async () => {
      if (!transition.isCurrent()) return undefined;
      return work(transition);
    });
    queue = task.then(
      () => undefined,
      () => undefined,
    );
    return task;
  };

  const cancel = () => {
    active?.controller.abort();
    active = null;
    generation += 1;
  };

  return {
    begin,
    currentGeneration: () => generation,
    isCurrent,
    run,
    cancel,
  };
}
