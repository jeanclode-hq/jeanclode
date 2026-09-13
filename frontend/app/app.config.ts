export default defineAppConfig({
  ui: {
    colors: {
      primary: 'neutral',
      neutral: 'neutral',
    },

    dashboardSidebar: {
      slots: {
        root: 'bg-sidebar-bg dark:bg-neutral-800 border-neutral-200 dark:border-neutral-700 transition-all duration-200 ease-out',
        header: 'h-14 shrink-0 border-b border-neutral-200 dark:border-neutral-700 transition-all duration-200',
        body: 'flex flex-col gap-1 px-2 py-3 transition-all duration-200',
        footer: 'shrink-0 border-t border-neutral-200 dark:border-neutral-700 px-2 py-2 transition-all duration-200',
      },
    },

    dashboardNavbar: {
      slots: {
        root: 'lg:hidden h-14 shrink-0 flex items-center gap-3 px-4 border-b border-neutral-200 dark:border-neutral-700 bg-default',
      },
    },

    dashboardPanel: {
      slots: {
        root: 'relative flex flex-col flex-1 min-w-0 min-h-svh lg:min-h-0 overflow-y-auto dither-bg dark:bg-[#0f1214]',
        header: '',
        body: 'flex flex-col flex-1 min-h-0 px-4 sm:px-6 lg:px-8 py-6',
      },
    },

    navigationMenu: {
      slots: {
        root: 'flex flex-col gap-0.5',
        item: 'group',
        link: 'cursor-pointer relative flex items-center gap-2.5 rounded-lg mx-1 my-px px-2.5 py-1.5 text-[13px] font-medium transition-all duration-150 ease-out text-neutral-600 dark:text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-700 hover:text-neutral-800 dark:hover:text-neutral-200 data-[active]:bg-neutral-200 dark:data-[active]:bg-neutral-700 data-[active]:text-neutral-800 dark:data-[active]:text-neutral-100 data-[active]:font-semibold',
        linkLeadingIcon: 'size-[18px] shrink-0 text-neutral-400 dark:text-neutral-500 group-hover:text-neutral-600 dark:group-hover:text-neutral-300 group-data-[active]:text-neutral-800 dark:group-data-[active]:text-neutral-100 transition-colors duration-150',
        linkLabel: 'truncate',
        linkTrailingBadge: 'ml-auto text-[11px] font-semibold bg-neutral-200 text-neutral-600 dark:bg-neutral-600 dark:text-neutral-200 px-1.5 py-0.5 rounded-full',
      },
    },

    button: {
      slots: {
        base: 'font-medium transition-all duration-150 cursor-pointer active:scale-[0.97] disabled:opacity-40 disabled:cursor-not-allowed disabled:active:scale-100 aria-disabled:opacity-40',
        trailingIcon: 'transition-transform duration-150 group-hover:translate-x-0.5',
      },
      variants: {
        variant: {
          solid: 'shadow-sm',
          outline: 'ring-1 ring-inset',
          soft: '',
          subtle: '',
          ghost: '',
          link: 'underline-offset-4 hover:underline',
        },
        size: {
          xs: { base: 'h-6 px-2 py-0.5 text-xs' },
          sm: { base: 'h-7 px-2.5 py-1 text-sm' },
          md: { base: 'h-8 px-3 py-1.5 text-sm' },
          lg: { base: 'h-9 px-4 py-2 text-base' },
          xl: { base: 'h-10 px-5 py-2.5 text-base' },
        },
      },
      compoundVariants: [
        {
          color: 'primary',
          variant: 'solid',
          class: 'bg-neutral-900 text-white hover:bg-neutral-950 active:bg-black dark:bg-neutral-100 dark:text-neutral-900 dark:hover:bg-neutral-200 dark:active:bg-neutral-300',
        },
        {
          color: 'primary',
          variant: 'outline',
          class: 'ring-neutral-300 text-neutral-700 hover:bg-neutral-100 hover:ring-neutral-400 dark:ring-neutral-600 dark:text-neutral-300 dark:hover:bg-neutral-700 dark:hover:ring-neutral-600',
        },
        {
          color: 'primary',
          variant: 'soft',
          class: 'bg-neutral-100 text-neutral-700 hover:bg-neutral-200 dark:bg-neutral-700 dark:text-neutral-300 dark:hover:bg-neutral-600',
        },
        {
          color: 'primary',
          variant: 'ghost',
          class: 'text-neutral-600 hover:bg-neutral-100 hover:text-neutral-800 dark:text-neutral-400 dark:hover:bg-neutral-700 dark:hover:text-neutral-200',
        },
      ],
      defaultVariants: {
        color: 'primary',
        variant: 'solid',
        size: 'md',
      },
    },

    input: {
      slots: {
        root: '',
        base: 'w-full rounded border-0 bg-white dark:bg-neutral-700 text-neutral-800 dark:text-neutral-100 placeholder:text-neutral-400 dark:placeholder:text-neutral-500 focus:outline-none ring-1 ring-inset ring-neutral-300 dark:ring-neutral-700 transition-shadow',
      },
      variants: {
        size: {
          sm: { base: 'px-2.5 py-1.5 text-sm' },
          md: { base: 'px-3 py-2 text-sm' },
          lg: { base: 'px-4 py-2.5 text-base' },
        },
      },
      defaultVariants: {
        size: 'md',
      },
    },

    textarea: {
      slots: {
        base: 'w-full rounded border-0 bg-white dark:bg-neutral-700 text-neutral-800 dark:text-neutral-100 placeholder:text-neutral-400 dark:placeholder:text-neutral-500 focus:outline-none ring-1 ring-inset ring-neutral-300 dark:ring-neutral-700 transition-shadow',
      },
    },

    select: {
      slots: {
        base: 'cursor-pointer w-full rounded border-0 bg-white dark:bg-neutral-700 text-neutral-800 dark:text-neutral-100 ring-1 ring-inset ring-neutral-300 dark:ring-neutral-700 focus:ring-1 focus:ring-inset focus:ring-neutral-500 dark:focus:ring-neutral-500',
        content: 'bg-white dark:bg-neutral-700 border border-neutral-200 dark:border-neutral-600',
        item: 'cursor-pointer px-3 py-2 text-neutral-600 dark:text-neutral-300 hover:bg-neutral-100 dark:hover:bg-neutral-600 data-[highlighted]:bg-neutral-100 dark:data-[highlighted]:bg-neutral-600',
      },
    },

    selectMenu: {
      slots: {
        base: 'cursor-pointer w-full rounded border-0 bg-white dark:bg-neutral-700 text-neutral-800 dark:text-neutral-100 ring-1 ring-inset ring-neutral-300 dark:ring-neutral-700 focus:ring-1 focus:ring-inset focus:ring-neutral-500 dark:focus:ring-neutral-500',
        content: 'bg-white dark:bg-neutral-700 border border-neutral-200 dark:border-neutral-600',
        item: 'cursor-pointer px-3 py-2 text-neutral-600 dark:text-neutral-300 hover:bg-neutral-100 dark:hover:bg-neutral-600 data-[highlighted]:bg-neutral-100 dark:data-[highlighted]:bg-neutral-600',
      },
    },

    switch: {
      slots: {
        base: 'cursor-pointer',
      },
    },

    checkbox: {
      slots: {
        base: 'cursor-pointer',
      },
    },

    radio: {
      slots: {
        base: 'cursor-pointer',
      },
    },

    radioGroup: {
      slots: {
        item: 'cursor-pointer',
      },
    },

    tabs: {
      slots: {
        trigger: 'cursor-pointer',
      },
    },

    accordion: {
      slots: {
        trigger: 'cursor-pointer',
      },
    },

    table: {
      slots: {
        root: 'w-full',
        thead: 'border-b border-neutral-200 dark:border-neutral-600',
        tbody: 'divide-y divide-neutral-100 dark:divide-neutral-700',
        tr: 'hover:bg-neutral-100 dark:hover:bg-neutral-700/60 transition-colors',
        th: 'px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-neutral-500 dark:text-neutral-300',
        td: 'px-4 py-4 text-sm text-neutral-700 dark:text-neutral-200',
      },
      variants: {
        striped: {
          true: {
            tr: 'even:bg-neutral-50 dark:even:bg-neutral-700/30',
          },
        },
      },
    },

    card: {
      slots: {
        root: 'bg-white dark:bg-neutral-700 rounded-xl border border-neutral-200 dark:border-neutral-700 shadow-sm',
        header: 'px-4 py-3 border-b border-neutral-200 dark:border-neutral-600',
        body: 'p-4',
        footer: 'px-4 py-3 border-t border-neutral-200 dark:border-neutral-600',
      },
      variants: {
        variant: {
          default: {},
          interactive: {
            root: 'hover:shadow-md hover:border-neutral-300 dark:hover:border-neutral-600 hover:-translate-y-0.5 transition-all duration-200 cursor-pointer',
          },
          stat: {
            root: 'hover:shadow-md hover:border-neutral-300 dark:hover:border-neutral-600 hover:-translate-y-0.5 transition-all duration-200 cursor-pointer',
          },
        },
        padding: {
          none: { body: 'p-0' },
          sm: { body: 'p-2' },
          md: { body: 'p-3' },
          lg: { body: 'p-4' },
          xl: { body: 'p-6' },
        },
      },
      defaultVariants: {
        variant: 'default',
        padding: 'lg',
      },
    },

    badge: {
      slots: {
        base: 'inline-flex items-center gap-1 rounded-full font-medium',
      },
      variants: {
        size: {
          xs: { base: 'px-2 py-0.5 text-xs' },
          sm: { base: 'px-2.5 py-0.5 text-sm' },
          md: { base: 'px-3 py-1 text-base' },
          lg: { base: 'px-3.5 py-1 text-md' },
        },
      },
      defaultVariants: {
        size: 'xs',
      },
    },

    pagination: {
      slots: {
        root: 'flex items-center gap-1',
        list: 'flex items-center gap-0.5',
        item: 'cursor-pointer size-7 flex items-center justify-center rounded text-sm font-medium text-neutral-500 dark:text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200 hover:bg-neutral-100 dark:hover:bg-neutral-800 transition-colors data-[selected]:bg-neutral-200 dark:data-[selected]:bg-neutral-700 data-[selected]:text-neutral-900 dark:data-[selected]:text-neutral-100',
        ellipsis: 'size-7 flex items-center justify-center text-neutral-400 dark:text-neutral-500',
        first: 'cursor-pointer size-7 text-neutral-400 dark:text-neutral-500 hover:text-neutral-600 dark:hover:text-neutral-300',
        prev: 'cursor-pointer size-7 text-neutral-400 dark:text-neutral-500 hover:text-neutral-600 dark:hover:text-neutral-300',
        next: 'cursor-pointer size-7 text-neutral-400 dark:text-neutral-500 hover:text-neutral-600 dark:hover:text-neutral-300',
        last: 'cursor-pointer size-7 text-neutral-400 dark:text-neutral-500 hover:text-neutral-600 dark:hover:text-neutral-300',
      },
    },

    alert: {
      slots: {
        root: 'flex gap-3 p-4 rounded-lg border',
        wrapper: 'flex-1 min-w-0',
        icon: 'size-5 shrink-0',
        title: 'font-medium',
        description: 'text-sm mt-1',
      },
    },

    dropdownMenu: {
      slots: {
        content: 'w-56 mb-2 bg-white dark:bg-neutral-700 border border-neutral-200 dark:border-neutral-600 shadow-lg',
        item: 'cursor-pointer text-neutral-600 dark:text-neutral-300 data-[highlighted]:bg-neutral-100 dark:data-[highlighted]:bg-neutral-600',
        itemLeadingIcon: 'text-neutral-500 dark:text-neutral-400',
        label: 'text-neutral-500 dark:text-neutral-400',
      },
    },

    tooltip: {
      slots: {
        content: 'h-auto max-w-xs px-2 py-1 text-xs font-medium bg-neutral-900 dark:bg-neutral-100 text-white dark:text-neutral-900 rounded shadow-lg',
        text: 'whitespace-normal break-words',
        arrow: 'fill-neutral-900 dark:fill-neutral-100',
      },
    },

    slideover: {
      slots: {
        overlay: 'bg-neutral-800/50 dark:bg-neutral-950/70',
        content: 'bg-white dark:bg-neutral-800 border-l border-neutral-200 dark:border-neutral-700',
      },
    },

    modal: {
      slots: {
        overlay: 'bg-neutral-800/50 dark:bg-neutral-950/70',
        content: 'bg-white dark:bg-neutral-700 rounded-lg border border-neutral-200 dark:border-neutral-600 shadow-xl',
        header: 'px-5 py-3 border-b border-neutral-200 dark:border-neutral-600',
        body: 'px-5 py-4',
        footer: 'px-5 py-3 border-t border-neutral-200 dark:border-neutral-600 flex justify-end gap-2',
      },
    },

    carousel: {
      slots: {
        root: 'relative overflow-hidden',
        viewport: 'overflow-hidden',
        container: 'flex',
        item: 'min-w-0 shrink-0 grow-0 basis-full',
        controls: 'absolute inset-0 flex items-center justify-between p-2',
        prev: 'cursor-pointer absolute left-2',
        next: 'cursor-pointer absolute right-2',
        dots: 'absolute bottom-2 left-1/2 -translate-x-1/2 flex gap-1',
        dot: 'cursor-pointer size-2 rounded-full bg-white/50 transition-colors data-[active]:bg-white',
      },
    },
  },
})
