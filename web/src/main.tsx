import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Toast } from '@heroui/react'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import './styles.css'
import { registerServiceWorker } from './push'
import { HeroRouter } from './router'
import { ConfirmProvider } from './ui'

void registerServiceWorker().catch((e) => console.error('service worker registration failed', e))

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <HeroRouter>
        <ConfirmProvider>
          <App />
        </ConfirmProvider>
        <Toast.Provider placement="top" />
      </HeroRouter>
    </BrowserRouter>
  </StrictMode>,
)
