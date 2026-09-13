export default defineAppConfig({
  ui: {
    colors: {
      primary: 'neutral',
    },
    prose: {
      callout: {
        variants: {
          color: {
            warning: {
              base: 'border border-warning/30 bg-warning/8 text-warning-900 dark:text-warning-200 [&_code]:text-warning-900 dark:[&_code]:text-warning-200 [&_code]:border-warning/30 [&>ul]:marker:text-warning/50',
              icon: 'text-warning-700 dark:text-warning-300',
              externalIcon: 'text-warning-700 dark:text-warning-300',
            },
            info: {
              base: 'border border-info/30 bg-info/8 text-info-900 dark:text-info-200 [&_code]:text-info-900 dark:[&_code]:text-info-200 [&_code]:border-info/30 [&>ul]:marker:text-info/50',
              icon: 'text-info-700 dark:text-info-300',
              externalIcon: 'text-info-700 dark:text-info-300',
            },
            error: {
              base: 'border border-error/30 bg-error/8 text-error-900 dark:text-error-200 [&_code]:text-error-900 dark:[&_code]:text-error-200 [&_code]:border-error/30 [&>ul]:marker:text-error/50',
              icon: 'text-error-700 dark:text-error-300',
              externalIcon: 'text-error-700 dark:text-error-300',
            },
          },
        },
      },
    },
  },
})
