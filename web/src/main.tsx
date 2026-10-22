import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Toast } from '@heroui/react'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { ErrorBoundary } from './ErrorBoundary'
import './styles.css'
import { registerServiceWorker } from './push'
import { listenForNavigation } from './swNavigate'
import { ConfirmProvider } from './ui'

listenForNavigation()
void registerServiceWorker().catch((e) => console.error('service worker registration failed', e))

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <ErrorBoundary>
        <ConfirmProvider>
          <App />
        </ConfirmProvider>
      </ErrorBoundary>
      <Toast.Provider placement="top" />
    </BrowserRouter>
  </StrictMode>,
)
