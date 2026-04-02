import React from 'react';

export const Button = React.forwardRef<HTMLButtonElement, React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'danger' | 'success' }>(
  ({ className = '', variant = 'primary', ...props }, ref) => {
    let bg = 'bg-blue-600 hover:bg-blue-700 text-white';
    if (variant === 'secondary') bg = 'bg-gray-200 hover:bg-gray-300 text-gray-800';
    if (variant === 'danger') bg = 'bg-red-600 hover:bg-red-700 text-white';
    if (variant === 'success') bg = 'bg-green-600 hover:bg-green-700 text-white';
    
    return (
      <button
        ref={ref}
        className={`px-4 py-2 rounded-lg font-medium transition-colors ${bg} ${className}`}
        {...props}
      />
    );
  }
);
Button.displayName = 'Button';

export const Card = ({ children, className = '' }: { children: React.ReactNode; className?: string }) => (
  <div className={`bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden ${className}`}>
    {children}
  </div>
);

export const StatusBadge = ({ status }: { status: string }) => {
  let color = 'bg-gray-100 text-gray-800';
  if (status === 'NEW') color = 'bg-blue-100 text-blue-800 border-blue-200';
  if (status === 'IN_PREP') color = 'bg-yellow-100 text-yellow-800 border-yellow-200';
  if (status === 'READY') color = 'bg-green-100 text-green-800 border-green-200';
  if (status === 'DELIVERED') color = 'bg-gray-200 text-gray-600 border-gray-300';
  if (status === 'CANCELLED') color = 'bg-red-100 text-red-800 border-red-200';

  return (
    <span className={`px-2.5 py-0.5 rounded-full text-xs font-semibold border ${color}`}>
      {status}
    </span>
  );
};
